module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire [LANES*DATA_W-1:0]  in_a_flat,
    input  wire                     in_a_flat_valid,
    output wire                     in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0]  in_x_flat,
    input  wire                     in_x_flat_valid,
    output wire                     in_x_flat_ready,
    output wire                     out_valid,
    input  wire                     out_ready,
    output wire signed [ACC_W-1:0]  out_c
);

    localparam XB     = COLS/LANES;              // x beats per transaction
    localparam AB     = ROWS*COLS/LANES;         // a beats per transaction
    localparam BPR    = COLS/LANES;              // a beats per output row
    localparam XB_W   = (XB   > 1) ? $clog2(XB)   : 1;
    localparam AB_W   = $clog2(AB + 1);
    localparam OUT_W  = (ROWS > 1) ? $clog2(ROWS) : 1;
    localparam LN_BPR = (BPR  > 1) ? $clog2(BPR)  : 1;
    localparam ROWS_W = (ROWS > 1) ? $clog2(ROWS) : 1;
    // exact width for a row dot product: max |sum| = COLS * 2^(2*DATA_W-2)
    localparam AW     = $clog2(COLS) + 2*DATA_W + 1;   // 23 bits, signed

    // x stored as XB banks of LANES elements; an a beat only reads one bank
    reg [LANES*DATA_W-1:0] x_bank [0:XB-1];
    // finished row sums, written exactly once per row per transaction
    reg signed [AW-1:0]    val   [0:ROWS-1];
    // running sum of the partial sums of the row currently being filled
    reg signed [AW-1:0]    temp;

    reg [XB_W:0]    x_cnt;         // x beats accepted, saturates at XB
    reg [AB_W-1:0]  a_cnt;         // a beats accepted, saturates at AB
    reg [ROWS_W:0]  rows_pending;  // completed rows not yet emitted
    reg [OUT_W-1:0] out_ptr;

    wire final_fire = out_valid && out_ready && (out_ptr == ROWS-1);
    wire a_fire     = in_a_flat_valid && in_a_flat_ready;
    wire x_fire     = in_x_flat_valid && in_x_flat_ready;
    wire o_fire     = out_valid && out_ready;

    wire [LN_BPR-1:0] beat_sub = a_cnt[LN_BPR-1:0];
    wire row_last = a_fire && (beat_sub == BPR-1);

    // an a beat may only fire once its x window has been buffered
    wire a_gate = (x_cnt > beat_sub);

    assign in_x_flat_ready = (x_cnt < XB);
    assign in_a_flat_ready = a_gate && (a_cnt < AB);
    assign out_valid       = (rows_pending != 0);
    assign out_c           = val[out_ptr];

    wire [AB_W-1:0]  beat    = (a_cnt >= AB) ? (AB-1) : a_cnt;
    wire [OUT_W-1:0] row_idx = beat >> LN_BPR;

    // 4:1 select of the x bank touched by the current a beat
    integer j;
    reg [LANES*DATA_W-1:0] lane_x;
    always @* begin
        lane_x = {(LANES*DATA_W){1'b0}};
        for (j = 0; j < XB; j = j + 1)
            if (beat_sub == j)
                lane_x = x_bank[j];
    end

    // 16 signed products accumulated into one per-beat partial sum
    integer k;
    reg signed [AW-1:0] partial;
    always @* begin
        partial = 0;
        for (k = 0; k < LANES; k = k + 1)
            partial = partial + $signed(in_a_flat[k*DATA_W +: DATA_W]) *
                                $signed(lane_x[k*DATA_W +: DATA_W]);
    end

    // single accumulator adder
    wire signed [AW-1:0] adder = temp + partial;

    integer idx;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_cnt        <= 0;
            a_cnt        <= 0;
            rows_pending <= 0;
            out_ptr      <= 0;
            temp         <= 0;
            for (idx = 0; idx < ROWS; idx = idx + 1)
                val[idx] <= 0;
        end else if (final_fire) begin
            // transaction complete: clear per-transaction state
            x_cnt        <= 0;
            a_cnt        <= 0;
            rows_pending <= 0;
            out_ptr      <= 0;
            temp         <= 0;
            for (idx = 0; idx < ROWS; idx = idx + 1)
                val[idx] <= 0;
        end else begin
            if (x_fire) begin
                x_bank[x_cnt] <= in_x_flat;
                x_cnt <= x_cnt + 1'b1;
            end
            if (a_fire) begin
                if (row_last) begin
                    val[row_idx] <= adder;
                    temp <= 0;
                    a_cnt <= (a_cnt == AB-1) ? AB : (a_cnt + 1'b1);
                end else begin
                    temp <= adder;
                    a_cnt <= a_cnt + 1'b1;
                end
            end
            if (o_fire)
                out_ptr <= out_ptr + 1'b1;

            if (row_last && o_fire)
                rows_pending <= rows_pending;
            else if (row_last)
                rows_pending <= rows_pending + 1'b1;
            else if (o_fire)
                rows_pending <= rows_pending - 1'b1;
        end
    end

endmodule
