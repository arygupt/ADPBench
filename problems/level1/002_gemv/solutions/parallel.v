// Sanity-check submission: beat-parallel matrix-vector product.
// Buffers x (COLS elements), then MACs one full beat of A per cycle with one
// multiplier per lane. One output word per row, rows streamed back to back.
// Repeated transactions clear x_done/row state when the last output word is
// accepted.

module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,

    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,

    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam X_BEATS   = COLS / LANES;
    localparam ROW_BEATS = COLS / LANES;

    localparam S_RECV = 2'd0, S_MAC = 2'd1, S_OUT = 2'd2;

    reg [1:0]  state;
    integer    x_beat;
    integer    row_beat;
    integer    row;
    integer    idx;
    reg        x_done;
    reg        a_have;
    reg signed [ACC_W-1:0]      acc;
    reg [DATA_W-1:0]            x_mem [0:COLS-1];
    reg [LANES*DATA_W-1:0]      a_hold;

    assign in_x_flat_ready = !x_done;
    assign in_a_flat_ready = (state == S_MAC) && !a_have;
    assign out_valid       = (state == S_OUT);
    assign out_c           = acc;

    // A beat's x slice is usable once that x beat has been captured.
    wire x_slice_ready = x_done || (x_beat > row_beat);

    reg signed [ACC_W-1:0] beat_sum;
    integer l;
    always @(*) begin
        beat_sum = {ACC_W{1'b0}};
        for (l = 0; l < LANES; l = l + 1) begin
            beat_sum = beat_sum
                     + $signed(a_hold[l*DATA_W +: DATA_W])
                     * $signed(x_mem[row_beat*LANES + l]);
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            state    <= S_RECV;
            x_beat   <= 0;
            row_beat <= 0;
            row      <= 0;
            x_done   <= 1'b0;
            a_have   <= 1'b0;
            acc      <= {ACC_W{1'b0}};
        end else begin
            // Capture x whenever offered, independently of the A stream.
            if (!x_done && in_x_flat_valid) begin
                for (idx = 0; idx < LANES; idx = idx + 1)
                    x_mem[x_beat*LANES + idx] <= in_x_flat[idx*DATA_W +: DATA_W];
                if (x_beat == X_BEATS - 1) begin
                    x_beat <= 0;
                    x_done <= 1'b1;
                end else begin
                    x_beat <= x_beat + 1;
                end
            end

            case (state)
                S_RECV: begin
                    if (x_done) state <= S_MAC;
                end

                S_MAC: begin
                    if (!a_have && in_a_flat_valid && in_a_flat_ready) begin
                        a_hold <= in_a_flat;
                        a_have <= 1'b1;
                    end
                    if (a_have && x_slice_ready) begin
                        acc    <= acc + beat_sum;
                        a_have <= 1'b0;
                        if (row_beat == ROW_BEATS - 1) begin
                            row_beat <= 0;
                            state    <= S_OUT;
                        end else begin
                            row_beat <= row_beat + 1;
                        end
                    end
                end

                S_OUT: begin
                    if (out_ready) begin
                        acc <= {ACC_W{1'b0}};
                        if (row == ROWS - 1) begin
                            row    <= 0;
                            x_done <= 1'b0;
                            state  <= S_RECV;
                        end else begin
                            row   <= row + 1;
                            state <= S_MAC;
                        end
                    end
                end

                default: state <= S_RECV;
            endcase
        end
    end

endmodule
