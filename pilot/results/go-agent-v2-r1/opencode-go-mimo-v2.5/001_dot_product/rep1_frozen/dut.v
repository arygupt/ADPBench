module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS = LEN / LANES;

    reg [3:0]  cnt;
    reg        state;
    reg signed [ACC_W-1:0] acc;

    // Independent buffers for each stream
    reg [LANES*DATA_W-1:0] buf_a, buf_b;
    reg                    buf_a_v, buf_b_v;

    // Accept into buffer when empty and not outputting
    assign in_a_flat_ready = (state == 1'b0) && !buf_a_v;
    assign in_b_flat_ready = (state == 1'b0) && !buf_b_v;

    // Process when both buffers full
    wire process = buf_a_v && buf_b_v && (state == 1'b0);

    assign out_valid = (state == 1'b1);
    assign out_c     = acc;

    // Combinational: sum of 32 signed 8x8 products from buffered data
    reg signed [ACC_W-1:0] partial_sum;
    integer j;
    always @(*) begin
        partial_sum = 0;
        for (j = 0; j < LANES; j = j + 1) begin
            partial_sum = partial_sum +
                $signed(buf_a[j*DATA_W +: DATA_W]) *
                $signed(buf_b[j*DATA_W +: DATA_W]);
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt    <= 0;
            state  <= 0;
            acc    <= 0;
            buf_a_v <= 0;
            buf_b_v <= 0;
        end else begin
            // Accept stream a into buffer
            if (in_a_flat_valid && in_a_flat_ready) begin
                buf_a   <= in_a_flat;
                buf_a_v <= 1'b1;
            end
            // Accept stream b into buffer
            if (in_b_flat_valid && in_b_flat_ready) begin
                buf_b   <= in_b_flat;
                buf_b_v <= 1'b1;
            end

            if (process) begin
                acc     <= acc + partial_sum;
                buf_a_v <= 1'b0;
                buf_b_v <= 1'b0;
                cnt     <= cnt + 1;
                if (cnt == BEATS - 1) begin
                    state <= 1'b1;
                    cnt   <= 0;
                end
            end else if (state == 1'b1 && out_valid && out_ready) begin
                state  <= 1'b0;
                cnt    <= 0;
                acc    <= 0;
            end
        end
    end

endmodule
